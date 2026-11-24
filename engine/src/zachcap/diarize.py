"""Speaker diarization with pyannote: who spoke when, plus one voice embedding per speaker."""

from __future__ import annotations

import gc
import os
import warnings
from dataclasses import dataclass

import numpy as np

from . import cuda, models
from .transcript import Turn

# pyannote reports usage (file durations, speaker counts) to its servers by
# default. ZachCap keeps everything on the machine, so turn that off first.
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"

MODEL = models.SPEAKER_MODEL.repo
# Voiceprints only compare within one embedding model; stored samples are tagged with it.
EMBEDDING_MODEL = MODEL
MODEL_PAGE = f"https://huggingface.co/{MODEL}"

SETTING = "faster_speakers"  # "1": separate speakers faster (FASTER_STEP)
# How far the speaker model's 10 s window moves between looks, as a share of it. pyannote's
# own (a tenth) is thorough; twice that takes a third to a half less time on a CPU (measured
# on a 6-core laptop CPU: a 2.4-minute synthetic track alone, 85 s at 0.1 and 46 s at 0.2;
# a whole meeting processed, 194 s and 135 s; the same speakers found), with the edges of
# each turn a little less exact.
FASTER_STEP = 0.2


def step_for(setting: str | None) -> float | None:
    """The step for the saved setting; None keeps the model's own."""
    return FASTER_STEP if setting == "1" else None


def set_step(pipeline, step: float) -> None:
    """Makes a speaker pipeline look every ``step`` of its window. The pipeline takes it
    only when it's built, so both of its copies are set."""
    pipeline.segmentation_step = step
    pipeline._segmentation.step = step * pipeline._segmentation.duration


class SpeakerModelUnavailable(RuntimeError):
    """No Hugging Face token, or the model's terms aren't accepted: transcribe without names."""


@dataclass
class Diarization:
    turns: list[Turn]  # without overlaps, for lining words up with speakers
    embeddings: dict[str, np.ndarray]  # one centroid per speaker label


class FixedBatchEmbedding:
    """Pads every speaker-embedding batch to the same size.

    pyannote's last batch is usually smaller than the rest. That new input shape makes
    cuDNN choose a convolution with a ~10 GB workspace; on a 4 GB GPU Windows quietly
    spills it into shared system memory and that single batch takes ~95 s instead of ~1 s.
    With a fixed shape every batch reuses the fast plan. Padded rows are dropped again.
    """

    def __init__(self, inner, batch_size: int) -> None:
        self.inner = inner
        self.batch_size = batch_size

    def __getattr__(self, name: str):
        return getattr(self.inner, name)

    def __call__(self, waveforms, masks=None):
        import torch

        count = waveforms.shape[0]
        missing = self.batch_size - count
        if missing > 0:
            waveforms = torch.cat([waveforms, waveforms.new_zeros((missing, *waveforms.shape[1:]))])
            if masks is not None:
                masks = torch.cat([masks, masks.new_ones((missing, *masks.shape[1:]))])
        return self.inner(waveforms, masks=masks)[:count]


class Diarizer:
    def __init__(self, token: str | None, step: float | None = None) -> None:
        gpu = cuda.prepare()
        import torch

        with warnings.catch_warnings():
            # torchcodec warns when FFmpeg isn't installed; audio is passed in memory instead.
            warnings.simplefilter("ignore")
            from pyannote.audio import Pipeline

        self._torch = torch
        try:
            pipeline = Pipeline.from_pretrained(
                MODEL, revision=models.SPEAKER_MODEL.revision, token=token or None
            )
        except Exception as exc:
            raise SpeakerModelUnavailable(_access_help(token)) from exc
        if pipeline is None:
            raise SpeakerModelUnavailable(_access_help(token))
        self.device = torch.device("cuda" if gpu else "cpu")
        self.pipeline = pipeline.to(self.device)
        if step is not None:
            set_step(self.pipeline, step)
        self.pipeline._embedding = FixedBatchEmbedding(
            self.pipeline._embedding, self.pipeline.embedding_batch_size
        )

    def run(self, audio: np.ndarray) -> Diarization:
        """Diarize 16 kHz mono audio."""
        waveform = self._torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32))[None]
        output = self.pipeline({"waveform": waveform, "sample_rate": 16000})

        embeddings: dict[str, np.ndarray] = {}
        centroids = output.speaker_embeddings
        for i, label in enumerate(output.speaker_diarization.labels()):
            if centroids is None or i >= len(centroids):
                continue
            vector = np.asarray(centroids[i], dtype=np.float32)
            # pyannote fills in zeros for speakers it couldn't embed; those match no one.
            if np.all(np.isfinite(vector)) and np.linalg.norm(vector) > 1e-6:
                embeddings[label] = vector
        turns = [
            Turn(segment.start, segment.end, label)
            for segment, _, label in output.exclusive_speaker_diarization.itertracks(
                yield_label=True
            )
        ]
        return Diarization(turns, embeddings)

    def close(self) -> None:
        del self.pipeline
        gc.collect()
        if self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()


def _access_help(token: str | None) -> str:
    if not token:
        return (
            "The speaker model needs a free Hugging Face token. Accept the conditions at "
            f"{MODEL_PAGE}, create a Read token, then run: zachcap keys set huggingface"
        )
    return (
        f"Couldn't load {MODEL}. Check that you accepted the conditions at {MODEL_PAGE} "
        "with the same account as your token."
    )
