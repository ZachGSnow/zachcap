"""Match meeting speakers to known people by voice embedding.

Each person has a few saved embeddings ("samples"), one per meeting where they
were confirmed. A meeting speaker's score for a person is the mean of its top-3
cosine similarities to that person's samples, and each speaker takes the person
it scores highest with. Diarization sometimes splits one voice into two speakers,
so two speakers may match the same person; both then show that person's name.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass

import numpy as np

# Calibrated on a real 37-minute Teams recording (pyannote community-1 embeddings):
# the same person scored 0.88-0.96 across halves, different people 0.07-0.52.
AUTO_THRESHOLD = 0.75
SUGGEST_THRESHOLD = 0.60


@dataclass(frozen=True)
class Match:
    person_id: int | None
    score: float
    status: str  # "auto" | "suggested" | "unknown"


def to_blob(vector: np.ndarray) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _unit(vector: np.ndarray) -> np.ndarray:
    return vector / (np.linalg.norm(vector) + 1e-9)


def person_score(embedding: np.ndarray, samples: list[np.ndarray], top_k: int = 3) -> float:
    if not samples:
        return -1.0
    sims = np.array([float(_unit(embedding) @ _unit(s)) for s in samples])
    return float(np.sort(sims)[::-1][:top_k].mean())


def match_speakers(
    speakers: dict[Hashable, np.ndarray],
    library: dict[int, list[np.ndarray]],
    auto: float = AUTO_THRESHOLD,
    suggest: float = SUGGEST_THRESHOLD,
) -> dict[Hashable, Match]:
    people = [p for p, samples in library.items() if samples]
    result = {}
    for key, embedding in speakers.items():
        scores = {person: person_score(embedding, library[person]) for person in people}
        best = max(scores, key=scores.get, default=None)
        score = scores.get(best, 0.0)
        if best is not None and score >= auto:
            result[key] = Match(best, score, "auto")
        elif best is not None and score >= suggest:
            result[key] = Match(best, score, "suggested")
        else:
            result[key] = Match(None, score, "unknown")
    return result
